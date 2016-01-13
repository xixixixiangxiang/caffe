#ifdef USE_MPI

#include "caffe/common.hpp"
#include "caffe/util/mpi_job_queue.hpp"
#include "caffe/util/mpi_templates.hpp"


using boost::mutex;

namespace caffe {

template <typename Dtype>
MPIJobQueue<Dtype>& MPIJobQueue<Dtype>::instance() {
  static MPIJobQueue<Dtype> instance;
  return instance;
}

template <typename Dtype>
MPIJobQueue<Dtype>::MPIJobQueue()
  : thread_started_(false) {
  try {
    thread_.reset(new boost::thread(&MPIJobQueue<Dtype>::ThreadFunc, this));
  } catch (...) {
    LOG(FATAL) << "Failed to start MPI job queue thread.";
  }
}

template <typename Dtype>
MPIJobQueue<Dtype>::~MPIJobQueue<Dtype>() {
  try {
    cv_work_.notify_one();
    thread_->join();
  } catch (...) {
    LOG(FATAL) << "Failed to release MPI job queue thread.";
  }
}

template <typename Dtype>
void MPIJobQueue<Dtype>::ThreadFunc() {
  thread_started_.store(true);
  while (true) {
    mutex::scoped_lock read_lock(queue_mutex_);
    while (queue_.empty()) {
      cv_work_.wait(read_lock);
    }
    read_lock.unlock();

    Dispatch(queue_.front());
    mutex::scoped_lock write_lock(queue_mutex_);
    queue_.pop();
    write_lock.unlock();
    cv_done_.notify_one();
  }
}

template <typename Dtype>
void MPIJobQueue<Dtype>::WaitAll() {
  mutex::scoped_lock lk(queue_mutex_);
  while (!queue_.empty()) {
    cv_done_.wait(lk);
  }
}

template <typename Dtype>
void MPIJobQueue<Dtype>::Push(const MPIJobQueue<Dtype>::Job& job) {
  while (!thread_started_.load());
  mutex::scoped_lock push_lock(queue_mutex_);
  queue_.push(job);
  push_lock.unlock();
  cv_work_.notify_one();
}

template <typename Dtype>
void MPIJobQueue<Dtype>::Dispatch(MPIJobQueue<Dtype>::Job& job) {
  switch (job.op) {
    case OP_SUM_ALL:
      MPIAllreduce<Dtype>(job.count,
          (job.src_ptr == job.dst_ptr) ? MPI_IN_PLACE : job.src_ptr,
          job.dst_ptr, MPI_SUM);
      break;
    default:
      LOG(FATAL) << "Unrecognized MPI job";
  }
}

INSTANTIATE_CLASS(MPIJobQueue);

}

#endif